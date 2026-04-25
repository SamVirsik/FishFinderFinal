import tifffile as tiff
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from PIL import Image
import random
import time
import scipy.sparse as sp

def readData(importName):
    tif_data = tiff.imread(importName)
    df = pd.DataFrame(tif_data)
    number_rows, number_columns = df.shape
    return df

def GPSConvert(latitude, longitude):
    p1, p2 = abs(latitude) - int(abs(latitude)), abs(longitude) - int(abs(longitude))
    p3, p4 = p1*60, p2*60
    p5, p6 = int(p3), int(p4)
    p7, p8 = round(((p3 - p5)*60), 2), round(((p4 - p6)*60), 2)
    northHeading, westHeading = str(int(abs(latitude))) + "°" + str(p5) + "'" + str(p7), str(int(abs(longitude))) + "°" + str(p6) + "'" + str(p8)
    nindex1, windex1, nindex2, windex2 = northHeading.index("°"), westHeading.index("°"), northHeading.index("'"), westHeading.index("'")
    n1p, n2p, n3p = northHeading[0:nindex1], northHeading[(nindex1+1): nindex2], northHeading[(nindex2+1):]
    w1p, w2p, w3p = westHeading[0:windex1], westHeading[(windex1+1):windex2], westHeading[(windex2+1):]
    p11, p22 = str(round(((((float(n3p))/60) + int(n2p))/60) + int(n1p), 7)), str(round(((((float(w3p))/60) + int(w2p))/60) + int(w1p), 7) *-1)
    p33, p44 = str(int(n1p)) + "." + str(round(((float(n3p)/60)+int(n2p)), 3)), str(int(w1p)) + "." + str(round(((float(w3p)/60)+int(w2p)), 3))
    print(p44 + ", " + p33)
    wh, nh = str(int(abs(latitude))) + "°" + str(p5) + "'" + str(p7) + r'"' + "W", str(int(abs(longitude))) + "°" + str(p6) + "'" + str(p8) + r'"' + "N"
    print(str(round(longitude, 7)) + ", " + str(round(latitude, 7)))
    print(nh + ", " + wh)

def addGPS(df, bottom_left_gps, top_right_gps, number_rows, number_columns):
    
    latitude_step = (top_right_gps[0] - bottom_left_gps[0]) / number_columns
    longitude_step = (bottom_left_gps[1] - top_right_gps[1]) / number_rows

    latitude_array = [bottom_left_gps[0] + (i * latitude_step) for i in range(number_columns)]
    longitude_array = [top_right_gps[1] + (j * longitude_step) for j in range(number_rows)]

    df_with_GPS = pd.DataFrame(df)
    df_with_GPS.index = longitude_array

    columns = df_with_GPS.columns.tolist()

    result_dict = dict(zip(columns, latitude_array))

    df_with_GPS = df_with_GPS.rename(columns=result_dict)

    return df_with_GPS

def zeroToOneFifty (df, number_rows, number_columns):
    
    start_time=time.time()
    
    #next 6 lines for progress print:
    rowOnePercent = number_rows/100
    currentRow = 0
    rowNeededToChange = rowOnePercent
    nextPercent = 1
    print("")
    print("--------------------------")
    
    image = Image.new('RGB', (number_columns, number_rows), color = 'white')
    pixels = image.load()
    rowNum = 0
    colNum = 0
    
    colorList = [(139, 69, 19), (212, 255, 1), (190, 255, 1), (166, 255, 17), (139, 255, 17), (115, 255, 49), (83, 255, 84), (53, 255, 114), (53, 255, 147), (53, 255, 199), (17, 255, 220), (0, 255, 249), (0, 245, 249), (0, 226, 249), (0, 212, 242), (0, 198, 242), (0, 182, 242), (0, 167, 242), (0, 150, 242), (0, 128, 242), (0, 110, 218), (0, 88, 218), (0, 61, 215), (0, 30, 215), (0, 0, 207), (0, 0, 187), (0, 0, 163), (0, 0, 140), (0, 0, 128), (0, 0, 108), (0, 0, 88)]
    
    for row in df.values:
        for value in row:
            mValue = df.at[rowNum, colNum] #meterValues
            fValue = mValue*3.28084 #foot value
            
            scaled = ((abs(fValue)) - fValue) / 2
            
            if scaled == 0.0:
                pixels[colNum, rowNum] = colorList[0]
            else:
                scaled = int(scaled/5) + 1
            
            if (scaled > len(colorList)-2):
                pixels[colNum, rowNum] = colorList[len(colorList)-1]
            else:
                pixels[colNum, rowNum] = colorList[int(scaled)]
            
            
            colNum+=1
        rowNum+=1
        colNum = 0
            
    finalPrint = "Progress: 100%"
    print('\r' + finalPrint)
    
    end_time = time.time()
    
    print("Total Time:" + str(end_time - start_time))
    
    return image

def get_subset(data_name, x_min = 0, x_max = 1, y_min=0, y_max=1):
    start_time = time.time_ns()
    print(0, (time.time_ns()-start_time)/1000000)
    
    #get data
    df = pd.DataFrame(tiff.imread(data_name+".tif"))
    
    excess_rows = len(df) - len(df.columns)
    excess_cols = len(df.columns) - len(df)

    if excess_rows > 0: 
        df = df.iloc[:-excess_rows]
    elif excess_cols > 0:
        df = df.iloc[:, :-excess_cols]

    #Choose selected area
    row_start = int(len(df)*y_min)
    col_start = int(len(df)*x_min)
    rows_to_include = int(len(df) * (y_max-y_min))
    cols_to_include = int(len(df) * (x_max-x_min))
    # cols_to_include = int(len(df.columns) * (y_max))

    df = df.iloc[row_start:row_start + rows_to_include, col_start:col_start + cols_to_include].reset_index(drop=True)
    #make image
    image = Image.new('RGB', df.shape, color = 'white')
    pixels = image.load()
    
    colorList = [(139, 69, 19), (212, 255, 1), (190, 255, 1), (166, 255, 17), (139, 255, 17), (115, 255, 49), (83, 255, 84), (53, 255, 114), (53, 255, 147), (53, 255, 199), (17, 255, 220), (0, 255, 249), (0, 245, 249), (0, 226, 249), (0, 212, 242), (0, 198, 242), (0, 182, 242), (0, 167, 242), (0, 150, 242), (0, 128, 242), (0, 110, 218), (0, 88, 218), (0, 61, 215), (0, 30, 215), (0, 0, 207), (0, 0, 187), (0, 0, 163), (0, 0, 140), (0, 0, 128), (0, 0, 108), (0, 0, 88)]
    print(1, (time.time_ns()-start_time)/1000000)
    rowNum = 0
    colNum = 0
    for row in df.values:
        for colNum in range(len(row)):
            mValue = df.at[rowNum, colNum+col_start] #meterValues
            fValue = mValue*3.28084 #foot value
            
            scaled = ((abs(fValue)) - fValue) / 2
            
            if scaled == 0.0:
                pixels[colNum, rowNum] = colorList[0]
            else:
                scaled = int(scaled/5) + 1
            
            if (scaled > len(colorList)-2):
                x = colorList[len(colorList)-1]
                pixels[colNum, rowNum] = x
            else:
                x = colorList[int(scaled)]
                pixels[colNum, rowNum] = x
        rowNum+=1
    
    name = "SECTION_" + data_name + "_" + str(x_min) + "_" + str(x_max) + "_" + str(y_min) + "_" + str(y_max) + ".png"
    print(2, (time.time_ns()-start_time)/1000000)
    image.save("img/"+name, save_all = True)
    print(3, (time.time_ns()-start_time)/1000000)
    return name

#GetData

if __name__ == "__main__":
    print("How would you like to access the image?")
    print("Compute one now (c), or upload one (u) which has already been created?")
    imageSource = input()

    if imageSource == "c":
        print("Please enter the file name of the data import.")
        print("GPS data will automatically be taken from hardcoded values unless it is unknown.")
        nameImport = input()
        dfNOCOORD = readData(nameImport)

        nR, nC = dfNOCOORD.shape
        tempCopy = dfNOCOORD.copy()
        

        print("Please enter the GPS information manually.")
        print("")
        print("Please begin with the bottom left longitude. Should be roughly -81.")
        bllong = input()
        print("Bottom left latitude now. Should be roughly 24.")
        bllat = input()
        print("Top right longitude now.")
        trlong = input()
        print("Now top right latitude.")
        trlat = input()
        bottom_left_gps = (float(bllong), float(bllat))
        top_right_gps = (float(trlong), float(trlat))

        
        dfCOORD = addGPS(tempCopy, bottom_left_gps, top_right_gps, nR, nC)
        del tempCopy
        print("--------------------------")
        print("Which analysis type would you like to use?")
        print("")
        print("General: g")
        print("0 to 150, 5 foot incraments: 5")
        print("1 foot incraments up to 28 feet: 1")
        print("Contour map V1: c1")
        print("Contour map V1 with general backing: c1g")
        print("Contour map V2: c2")
        typeAnalysis = input()

        if typeAnalysis == "g":
            myImage = roughDivide(dfNOCOORD, nR, nC)
        elif typeAnalysis == "5":
            myImage = zeroToOneFifty(dfNOCOORD, nR, nC)
        elif typeAnalysis == "1":
            myImage = oneFootSteps(dfNOCOORD, nR, nC)
        elif typeAnalysis == "c1":
            myImage = contourMapV1(dfNOCOORD, nR, nC)
        elif typeAnalysis == "c1g":
            myImage = contourMapV1General(dfNOCOORD, nR, nC)
        elif typeAnalysis == "c2":
            print("--------------------------")
            print("The algorithm you chose uses a rolling average.")
            print("What would you like the diameter of the roll to be?")
            print("You should enter an odd number, since the diameter includes the desired pixel.")
            size = int(input())
            myImage = contourMapV2(dfNOCOORD, nR, nC, size)
        else:
            raise Exception("INVALID INPUT - Please Run Again")
        
        print("--------------------------")
        print("")
        print("Computations are complete.")
        print("Would you like to export the image? (yes or no)")
        download = input()
        
        if download == 'yes' or download == "Yes":
            myImage.save('RecentExport.png', save_all = True)
            print("--------------------------")
            print("The image has been exported.")
        else:
            print("--------------------------")
            print("Ok, the image was not exported.")
            print("")
            print("The image is ready for manipulation.")
            print("Continue to the next code block.")
            
    elif imageSource == "u":
        print("What is the name of the upload?")
        uploadName = input()
        myImage = Image.open(uploadName)
        
        corrdinates = get2GPS(uploadName)
        if corrdinates[4] == True:
            bottom_left_gps = (corrdinates[0], corrdinates[1])
            top_right_gps = (corrdinates[2], corrdinates[3])
        elif corrdinates[4] == False:
            print("It seems we don't have GPS data for that upload.")
            print("Please enter the GPS information manually.")
            print("")
            print("Please begin with the bottom left longitude. Should be roughly -81.")
            bllong = input()
            print("Bottom left latitude now. Should be roughly 24.")
            bllat = input()
            print("Top right longitude now.")
            trlong = input()
            print("Now top right latitude.")
            trlat = input()
            bottom_left_gps = (float(bllong), float(bllat))
            top_right_gps = (float(trlong), float(trlat))
        else:
            raise Exception("SOMETHING WENT WRONG - Please Run Again")
        
        pixels = myImage.load() #hahaha using the pixels as if it were the data cuz the values dont matter and the index and columns are the same
        
        
        #This next part is for finding the size of the image, then making a blank dataframe to represent it. 
        hitNull = False
        iteration = 0
        count = 0
        while hitNull == False:
            try:
                x = pixels[0, iteration]
                iteration+=1
                count+=1
            except:
                hitNull = True
        rowNum = count
        
        hitNull = False
        iteration = 0
        count = 0
        while hitNull == False:
            try:
                x = pixels[iteration, 0]
                iteration+=1
                count+=1
            except:
                hitNull = True
        colNum = count
        
        dfNOCOORD = pd.DataFrame(0, index = range(rowNum), columns = range(colNum))
        nR, nC = dfNOCOORD.shape
        tempCopy = dfNOCOORD.copy()
        dfCOORD = addGPS(tempCopy, bottom_left_gps, top_right_gps, nR, nC)
        del tempCopy
        
        print("--------------------------")
        print("Computations are complete.")
        
    else:
        raise Exception("INVALID INPUT - Please Run Again")

    #img_4_general.tif

    myImage.show()